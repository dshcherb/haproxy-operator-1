#!/usr/bin/env python3

import logging
import sys

sys.path.append('lib') # noqa

import subprocess

from ops.charm import CharmBase
from ops.main import main
from ops.framework import StoredState
from ops.model import ActiveStatus, BlockedStatus

from jinja2 import Environment, FileSystemLoader
from pathlib import Path
from interface_proxy_listen_tcp import ProxyListenTcpInterfaceRequires
from interface_vrrp_parameters import (
    VRRPParametersProvides,
    VRRPInstance,
    VRRPScript,
)

logger = logging.getLogger(__name__)


class HaproxyCharm(CharmBase):

    state = StoredState()

    HAPROXY_ENV_FILE = Path('/etc/default/haproxy')

    def __init__(self, *args):
        super().__init__(*args)

        self.state.set_default(started=False)

        self.haproxy_conf_file = Path(f'/etc/haproxy/juju-{self.app.name}.cfg')

        self.framework.observe(self.on.install, self.on_install)
        self.framework.observe(self.on.start, self.on_start)
        self.framework.observe(self.on.stop, self.on_stop)
        self.framework.observe(self.on.config_changed, self.on_config_changed)

        self.tcp_backends = ProxyListenTcpInterfaceRequires(self, 'proxy-listen-tcp')
        self.framework.observe(self.tcp_backends.on.backends_changed, self.on_backends_changed)

        self.keepalived = VRRPParametersProvides(self, 'vrrp-parameters')
        self.framework.observe(self.keepalived.on.keepalived_available,
                               self.on_keepalived_available)

    def on_install(self, event):
        subprocess.check_call(['apt', 'update'])
        subprocess.check_call(['apt', 'install', '-yq', 'haproxy'])

        ctxt = {'haproxy_app_config': self.haproxy_conf_file}
        env = Environment(loader=FileSystemLoader('templates'))
        template = env.get_template('haproxy.env.j2')
        rendered_content = template.render(ctxt)
        self.HAPROXY_ENV_FILE.write_text(rendered_content)
        self.haproxy_conf_file.write_text('')

    def on_start(self, event):
        if not self.state.started:
            subprocess.check_call(['systemctl', 'start', 'haproxy'])
            self.state.started = True

        self.model.unit.status = ActiveStatus()

    def on_stop(self, event):
        if self.state.started:
            # TODO: handle the new "remove" hook https://github.com/juju/juju/pull/11237
            subprocess.check_call(['systemctl', 'stop', 'haproxy'])
            self.state.started = False

    def on_config_changed(self, event):
        self.reconfigure_haproxy()
        self.reconfigure_keepalived()
        subprocess.check_call(['systemctl', 'restart', 'haproxy'])

    def on_backends_changed(self, event):
        self.reconfigure_haproxy()
        self.reconfigure_keepalived()

    def reconfigure_haproxy(self):
        ctxt = {
            'listen_proxies': self.tcp_backends.listen_proxies
        }
        env = Environment(loader=FileSystemLoader('templates'))
        template = env.get_template('haproxy.conf.j2')
        rendered_content = template.render(ctxt)
        self.haproxy_conf_file.write_text(rendered_content)
        subprocess.check_call(['systemctl', 'restart', 'haproxy'])

    def on_keepalived_available(self, event):
        if not self.state.started:
            event.defer()
        self.reconfigure_keepalived()

    def reconfigure_keepalived(self):
        if not self.keepalived.is_joined:
            return

        # TODO: the check source into a separate file.
        vrrp_scripts = []
        for port in self.tcp_backends.frontend_ports:
            vrrp_scripts.append(VRRPScript(f'haproxy_port_{port}_check',
                                           f'''script "bash -c '</dev/tcp/127.0.0.1/{port}'"'''))
        # TODO: there needs to be a better way to determine an egress-facing network interface
        # on which to configure a virtual IP than this.
        vip_interface = self.model.get_binding('website').network.interfaces[0].name
        virtual_ip = self.model.config.get('virtual-ip')
        if virtual_ip is None:
            self.unit.status = BlockedStatus('Waiting for an administrator to set virtual-ip.')
            return
        vrrp_instance = VRRPInstance(self.app.name,
                                     self.model.config['virtual-router-id'],
                                     [virtual_ip],
                                     vip_interface,
                                     track_interfaces=[vip_interface],
                                     track_scripts=vrrp_scripts)
        self.keepalived.configure_vrrp_instances([vrrp_instance])
        self.unit.status = ActiveStatus()


if __name__ == '__main__':
    main(HaproxyCharm)
