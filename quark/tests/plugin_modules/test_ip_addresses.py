# Copyright 2013 Openstack Foundation
# All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
#  under the License.

import contextlib

import mock
from mock import patch
from neutron.common import exceptions
from oslo_config import cfg
import webob

from quark.db import models
from quark import exceptions as quark_exceptions
from quark.plugin_modules import ip_addresses
from quark.tests import test_quark_plugin


def _port_associate_stub(context, ports, address, **kwargs):
    for port in ports:
        assoc = models.PortIpAssociation()
        assoc.port_id = port.id
        assoc.ip_address_id = address.id
        assoc.port = port
        # NOTE(thomasem): This causes address['associations'] to gain this
        # PortIpAssocation instance.
        assoc.ip_address = address
        assoc.enabled = address.address_type == "fixed"
    return address


def _port_disassociate_stub(context, ports, address):
    port_ids = [port.id for port in ports]
    for idx, assoc in enumerate(address['associations']):
        if assoc.port_id in port_ids:
            address.associations.pop(idx)
    return address


def _ip_deallocate_stub(context, address):
    address['deallocated'] = 1
    address['address_type'] = None


class TestIpAddresses(test_quark_plugin.TestQuarkPlugin):
    @contextlib.contextmanager
    def _stubs(self, port, addr):
        port_model = None
        addr_model = None
        if port:
            port_model = models.Port()
            port_model.update(port)
        if addr:
            addr_model = models.IPAddress()
            addr_model.update(addr)

        def _alloc_ip(context, new_addr, net_id, port_m, *args, **kwargs):
            new_addr.extend([addr_model])

        with contextlib.nested(
            mock.patch("quark.db.api.network_find"),
            mock.patch("quark.db.api.port_find"),
            mock.patch(
                "quark.plugin_modules.ip_addresses.ipam_driver"),
            mock.patch(
                "quark.plugin_modules.ip_addresses.db_api"
                ".port_associate_ip"),
            mock.patch(
                "quark.plugin_modules.ip_addresses"
                ".validate_ports_on_network_and_same_segment")
        ) as (net_f, port_find, mock_ipam, mock_port_associate_ip, validate):
            port_find.return_value = port_model
            mock_ipam.allocate_ip_address.side_effect = _alloc_ip
            mock_port_associate_ip.side_effect = _port_associate_stub
            yield

    def test_create_ip_address_by_network_and_device(self):
        port = dict(id=1, network_id=2, ip_addresses=[])
        ip = dict(id=1, address=3232235876, address_readable="192.168.1.100",
                  subnet_id=1, network_id=2, version=4, used_by_tenant_id=1)
        with self._stubs(port=port, addr=ip):
            ip_address = {"network_id": ip["network_id"],
                          "version": 4, 'device_ids': [2]}
            response = self.plugin.create_ip_address(
                self.context, dict(ip_address=ip_address))
            self.assertIsNotNone(response["id"])
            self.assertEqual(response["network_id"], ip_address["network_id"])
            self.assertEqual(response["port_ids"], [port["id"]])
            self.assertEqual(response["subnet_id"], ip["subnet_id"])
            self.assertEqual(response['type'], None)
            self.assertEqual(response["version"], 4)
            self.assertEqual(response["address"], "192.168.1.100")
            self.assertEqual(response["tenant_id"], 1)

    def test_create_ip_address_with_port(self):
        port = dict(id=1, network_id=2, ip_addresses=[])
        ip = dict(id=1, address=3232235876, address_readable="192.168.1.100",
                  subnet_id=1, network_id=2, version=4)
        with self._stubs(port=port, addr=ip):
            ip_address = dict(port_ids=[port["id"]])
            ip_address['version'] = 4
            ip_address['network_id'] = 2
            response = self.plugin.create_ip_address(
                self.context, dict(ip_address=ip_address))

            self.assertIsNotNone(response['id'])
            self.assertEqual(response['network_id'], ip["network_id"])
            self.assertEqual(response['port_ids'], [port["id"]])
            self.assertEqual(response['subnet_id'], ip['id'])

    def test_create_ip_address_by_device_no_network_fails(self):
        with self._stubs(port={}, addr=None):
            ip_address = dict(device_ids=[4], version=4)
            with self.assertRaises(exceptions.BadRequest):
                self.plugin.create_ip_address(self.context,
                                              dict(ip_address=ip_address))

    def test_create_ip_address_invalid_missing_port_and_device_list(self):
        with self._stubs(port=None, addr=None):
            with self.assertRaises(exceptions.BadRequest):
                ip_address = {'ip_address': {'network_id': 'fake',
                                             'version': 4}}
                self.plugin.create_ip_address(self.context, ip_address)

    def test_create_ip_address_invalid_port(self):
        with self._stubs(port=None, addr=None):
            with self.assertRaises(exceptions.PortNotFound):
                ip_address = {
                    'ip_address': {
                        'port_ids': ['fake'],
                        'version': 4,
                        'network_id': 'fake'
                    }
                }
                self.plugin.create_ip_address(self.context, ip_address)


class TestCreateIpAddressQuotaCheck(test_quark_plugin.TestQuarkPlugin):
    @contextlib.contextmanager
    def _stubs(self, port, addresses):
        port_model = models.Port()
        port_model.update(port)

        for addr in addresses:
            addr_model = models.IPAddress()
            addr_model.update(addr)
            port_model["ip_addresses"].append(addr_model)

        with contextlib.nested(
            mock.patch("quark.db.api.network_find"),
            mock.patch("quark.db.api.port_find"),
            mock.patch("quark.plugin_modules.ip_addresses.ipam_driver"),
            mock.patch("quark.plugin_modules.ip_addresses.db_api"
                       ".port_associate_ip"),
            mock.patch("quark.plugin_modules.ip_addresses"
                       ".validate_ports_on_network_and_same_segment")
        ) as (net_f, port_find, mock_ipam, mock_port_associate_ip, validate):
            port_find.return_value = port_model
            yield

    def test_create_ip_address_with_port_over_quota(self):
        addresses = [{"id": ip, "address": ip} for ip in xrange(5)]
        port = dict(id=1, network_id=2, ip_addresses=[])

        ip = dict(id=1, address=3232235876, address_readable="192.168.1.100",
                  subnet_id=1, network_id=2, version=4)

        with self._stubs(port=port, addresses=addresses):
            ip_address = dict(port_ids=[port["id"]])
            ip_address['version'] = 4
            ip_address['network_id'] = 2

            with self.assertRaises(exceptions.OverQuota):
                self.plugin.create_ip_address(
                    self.context, dict(ip_address=ip_address))


@mock.patch("quark.plugin_modules.ip_addresses.v")
@mock.patch("quark.plugin_modules.ip_addresses"
            ".validate_ports_on_network_and_same_segment")
@mock.patch("quark.plugin_modules.ip_addresses.ipam_driver")
@mock.patch("quark.plugin_modules.ip_addresses.db_api")
class TestQuarkSharedIPAddressCreate(test_quark_plugin.TestQuarkPlugin):
    def _alloc_stub(self, ip_model):
        def _alloc_ip(context, addr, *args, **kwargs):
            addr.append(ip_model)
        return _alloc_ip

    def test_create_ip_address_calls_port_associate_ip(self, mock_dbapi,
                                                       mock_ipam, *args):
        port = dict(id=1, network_id=2, ip_addresses=[])
        ip = dict(id=1, address=3232235876, address_readable="192.168.1.100",
                  subnet_id=1, network_id=2, version=4, tenant_id=1)
        port_model = models.Port()
        port_model.update(port)
        ip_model = models.IPAddress()
        ip_model.update(ip)

        mock_dbapi.port_find.return_value = port_model
        mock_ipam.allocate_ip_address.side_effect = (
            self._alloc_stub(ip_model))
        ip_address = {"network_id": ip["network_id"],
                      "version": 4, 'device_ids': [2],
                      "port_ids": [1]}

        self.plugin.create_ip_address(self.context,
                                      dict(ip_address=ip_address))
        mock_dbapi.port_associate_ip.assert_called_once_with(
            self.context, [port_model], ip_model)

    def test_create_ip_address_address_type_shared(self, mock_dbapi, mock_ipam,
                                                   *args):
        cfg.CONF.set_override('ipam_reuse_after', 100, "QUARK")
        ports = [dict(id=1, network_id=2, ip_addresses=[], service="none"),
                 dict(id=2, network_id=2, ip_addresses=[], service="none")]
        ip = dict(id=1, address=3232235876, address_readable="192.168.1.100",
                  subnet_id=1, network_id=2, version=4, tenant_id=1)
        port_models = [models.Port(**p) for p in ports]
        ip_model = models.IPAddress()
        ip_model.update(ip)
        mock_dbapi.port_find.side_effect = port_models
        mock_ipam.allocate_ip_address.side_effect = (
            self._alloc_stub(ip_model))

        ip_address = {"network_id": ip["network_id"],
                      "version": 4, 'device_ids': [2],
                      "port_ids": [pm.id for pm in port_models]}
        self.plugin.create_ip_address(self.context,
                                      dict(ip_address=ip_address))
        # NOTE(thomasem): Having to assert that [ip_model] was passed instead
        # of an empty list due to the expected behavior of this method being
        # that it mutates the passed in list. So, after it's run, the list
        # has already been mutated and it's a reference to that list that
        # we're checking. This method ought to be changed to return the new
        # IP and let the caller mutate the list, not the other way around.
        mock_ipam.allocate_ip_address.assert_called_once_with(
            self.context, [ip_model], ip['network_id'], None, 100,
            version=ip_address['version'], ip_addresses=[],
            address_type="shared")

    def test_create_ip_address_address_type_fixed(self, mock_dbapi, mock_ipam,
                                                  *args):
        cfg.CONF.set_override('ipam_reuse_after', 100, "QUARK")
        ports = [dict(id=1, network_id=2, ip_addresses=[])]
        ip = dict(id=1, address=3232235876, address_readable="192.168.1.100",
                  subnet_id=1, network_id=2, version=4, tenant_id=1)
        port_models = [models.Port(**p) for p in ports]
        ip_model = models.IPAddress()
        ip_model.update(ip)
        mock_dbapi.port_find.side_effect = port_models
        mock_ipam.allocate_ip_address.side_effect = (
            self._alloc_stub(ip_model))

        ip_address = {"network_id": ip["network_id"],
                      "version": 4, 'device_ids': [2],
                      "port_ids": [pm.id for pm in port_models]}
        self.plugin.create_ip_address(self.context,
                                      dict(ip_address=ip_address))
        # NOTE(thomasem): Having to assert that [ip_model] was passed instead
        # of an empty list due to the expected behavior of this method being
        # that it mutates the passed in list. So, after it's run, the list
        # has already been mutated and it's a reference to that list that
        # we're checking. This method ought to be changed to return the new
        # IP and let the caller mutate the list, not the other way around.
        mock_ipam.allocate_ip_address.assert_called_once_with(
            self.context, [ip_model], ip['network_id'], None, 100,
            version=ip_address['version'], ip_addresses=[],
            address_type="fixed")

    def test_fail_to_make_shared_with_active_port(self, mock_dbapi, mock_ipam,
                                                  *args):
        cfg.CONF.set_override('ipam_reuse_after', 100, "QUARK")
        ports = [dict(id=1, network_id=2, ip_addresses=[], service="active"),
                 dict(id=2, network_id=2, ip_addresses=[], service="none")]
        ip = dict(id=1, address=3232235876, address_readable="192.168.1.100",
                  subnet_id=1, network_id=2, version=4, used_by_tenant_id=1)
        port_models = [models.Port(**p) for p in ports]
        ip_model = models.IPAddress()
        ip_model.update(ip)
        mock_dbapi.port_find.side_effect = port_models
        mock_ipam.allocate_ip_address.side_effect = (
            self._alloc_stub(ip_model))

        ip_address = {"network_id": ip["network_id"],
                      "version": 4, 'device_ids': [2],
                      "port_ids": [pm.id for pm in port_models]}
        qe = quark_exceptions
        with self.assertRaises(qe.PortRequiresDisassociation):
            self.plugin.create_ip_address(self.context,
                                          dict(ip_address=ip_address))
        # NOTE(thomasem): Having to assert that [ip_model] was passed instead
        # of an empty list due to the expected behavior of this method being
        # that it mutates the passed in list. So, after it's run, the list
        # has already been mutated and it's a reference to that list that
        # we're checking. This method ought to be changed to return the new
        # IP and let the caller mutate the list, not the other way around.
        mock_ipam.allocate_ip_address.assert_called_once_with(
            self.context, [ip_model], ip['network_id'], None, 100,
            version=ip_address['version'], ip_addresses=[],
            address_type="shared")


class TestQuarkSharedIPAddressPortsValid(test_quark_plugin.TestQuarkPlugin):
    def test_validate_ports_on_network_raise_segment(self):
        mock_ports = [models.Port(id="1", network_id="2"),
                      models.Port(id="2", network_id="2")]
        mock_subnets = [models.Subnet(id="1", segment_id="2"),
                        models.Subnet(id="2", segment_id="3")]
        for i, subnet in enumerate(mock_subnets):
            mock_address = models.IPAddress(id="2", network_id="2")
            mock_address.subnet = subnet
            mock_ports[i].ip_addresses.append(mock_address)

        with self.assertRaises(exceptions.BadRequest):
            ip_addresses.validate_ports_on_network_and_same_segment(
                mock_ports, "2")

    def test_validate_ports_on_network_raise_segment_multiple_ips(self):
        mock_ports = [models.Port(id="1", network_id="2"),
                      models.Port(id="2", network_id="2")]
        mock_subnets = [models.Subnet(id="1", segment_id="2"),
                        models.Subnet(id="2", segment_id="3")]
        for i, subnet in enumerate(mock_subnets):
            mock_address = models.IPAddress(id="2", network_id="2")
            mock_address.subnet = subnet
            for x in xrange(i + 1):
                mock_ports[x].ip_addresses.append(mock_address)

        with self.assertRaises(exceptions.BadRequest):
            ip_addresses.validate_ports_on_network_and_same_segment(
                mock_ports, "2")

    def test_validate_ports_on_network_raise_network(self):
        mock_ports = [models.Port(id="1", network_id="2"),
                      models.Port(id="2", network_id="3")]
        mock_addresses = [models.IPAddress(id="1", network_id="2"),
                          models.IPAddress(id="2", network_id="3")]

        for i, ip_address in enumerate(mock_addresses):
            ip_address.subnet = models.Subnet(id="1", segment_id="2")
            mock_ports[i].ip_addresses.append(ip_address)

        with self.assertRaises(exceptions.BadRequest):
            ip_addresses.validate_ports_on_network_and_same_segment(
                mock_ports, "2")

    def test_validate_ports_on_network_valid(self):
        mock_ports = [models.Port(id="1", network_id="2"),
                      models.Port(id="2", network_id="2")]
        for p in mock_ports:
            p.ip_addresses.append(models.IPAddress(id="1", network_id="2"))
            p.ip_addresses[-1].subnet = models.Subnet(id="1", segment_id="1")

        r = ip_addresses.validate_ports_on_network_and_same_segment(
            mock_ports, "2")
        self.assertEqual(r, None)


class TestQuarkSharedIPAddress(test_quark_plugin.TestQuarkPlugin):
    def test_shared_ip_request(self):
        ip_address_mock = {"ip_address": {"port_ids": [1, 2, 3]}}
        r = ip_addresses._shared_ip_request(ip_address_mock)
        self.assertTrue(r)

    def test_shared_ip_request_false(self):
        ip_address_mock = {"ip_address": {"port_ids": [1]}}
        r = ip_addresses._shared_ip_request(ip_address_mock)
        self.assertFalse(r)

    def test_can_be_shared(self):
        mock_address = models.IPAddress(id="1", address=3232235876,
                                        address_readable="192.168.1.100",
                                        subnet_id="1", network_id="2",
                                        version=4, used_by_tenant_id="1")
        mock_assocs = []
        for x in xrange(3):
            assoc = models.PortIpAssociation()
            assoc.ip_address_id = mock_address.id
            assoc.ip_address = mock_address
            assoc.enabled = [False, False, False][x]
            mock_assocs.append(assoc)
        r = ip_addresses._can_be_shared(mock_address)
        self.assertTrue(r)

    def test_can_be_shared_false(self):
        mock_address = models.IPAddress(id="1", address=3232235876,
                                        address_readable="192.168.1.100",
                                        subnet_id="1", network_id="2",
                                        version=4, used_by_tenant_id="1")
        mock_assocs = []
        for x in xrange(3):
            assoc = models.PortIpAssociation()
            assoc.ip_address_id = mock_address.id
            assoc.ip_address = mock_address
            assoc.enabled = [False, True, False][x]
            mock_assocs.append(assoc)
        r = ip_addresses._can_be_shared(mock_address)
        self.assertFalse(r)

    @mock.patch("quark.plugin_modules.ip_addresses._shared_ip_request")
    @mock.patch("quark.plugin_modules.ip_addresses._can_be_shared")
    def test_raise_if_shared_and_enabled(self, can_be_shared_mock,
                                         shared_ip_request_mock):
        can_be_shared_mock.return_value = False
        shared_ip_request_mock.return_value = True
        obj = mock.MagicMock()
        with self.assertRaises(exceptions.BadRequest):
            ip_addresses._raise_if_shared_and_enabled(obj, obj)

    @mock.patch("quark.plugin_modules.ip_addresses._shared_ip_request")
    @mock.patch("quark.plugin_modules.ip_addresses._can_be_shared")
    def test_raise_if_shared_and_enabled_noraise(self, can_be_shared_mock,
                                                 shared_ip_request_mock):
        can_be_shared_mock.return_value = True
        shared_ip_request_mock.return_value = True
        obj = mock.MagicMock()
        r = ip_addresses._raise_if_shared_and_enabled(obj, obj)
        self.assertEqual(r, None)

    @mock.patch("quark.plugin_modules.ip_addresses._shared_ip_request")
    @mock.patch("quark.plugin_modules.ip_addresses._can_be_shared")
    def test_raise_if_shared_and_enabled_fixed_request(self,
                                                       can_be_shared_mock,
                                                       shared_ip_request_mock):
        can_be_shared_mock.return_value = True
        shared_ip_request_mock.return_value = False
        obj = mock.MagicMock()
        r = ip_addresses._raise_if_shared_and_enabled(obj, obj)
        self.assertEqual(r, None)

    @mock.patch("quark.plugin_modules.ip_addresses._shared_ip_request")
    @mock.patch("quark.plugin_modules.ip_addresses._can_be_shared")
    def test_raise_if_shared_and_enabled_fixed_request_and_not_shareable(
            self, can_be_shared_mock, shared_ip_request_mock):
        can_be_shared_mock.return_value = False
        shared_ip_request_mock.return_value = False
        obj = mock.MagicMock()
        r = ip_addresses._raise_if_shared_and_enabled(obj, obj)
        self.assertEqual(r, None)


class TestQuarkUpdateIPAddress(test_quark_plugin.TestQuarkPlugin):

    @contextlib.contextmanager
    def _stubs(self, ports, addr, addr_ports=False):
        port_models = []
        addr_model = None

        for port in ports:
            port_model = models.Port()
            port_model.update(port)
            port_models.append(port_model)
        if addr:
            addr_model = models.IPAddress()
            addr_model.update(addr)
            if addr_ports:
                addr_model.ports = port_models

        db_mod = "quark.db.api"
        with contextlib.nested(
            mock.patch("%s.port_find" % db_mod),
            mock.patch("%s.ip_address_find" % db_mod),
            mock.patch("%s.port_associate_ip" % db_mod),
            mock.patch("%s.port_disassociate_ip" % db_mod),
            mock.patch("quark.plugin_modules.ip_addresses"
                       ".validate_ports_on_network_and_same_segment"),
            mock.patch("quark.plugin_modules.ip_addresses.ipam_driver")
        ) as (port_find, ip_find, port_associate_ip,
              port_disassociate_ip, val, mock_ipam):
            port_find.return_value = port_models
            ip_find.return_value = addr_model
            port_associate_ip.side_effect = _port_associate_stub
            port_disassociate_ip.side_effect = _port_disassociate_stub
            mock_ipam.deallocate_ip_address.side_effect = (
                _ip_deallocate_stub)
            yield

    def test_update_ip_address_does_not_exist(self):
        with self._stubs(ports=[], addr=None):
            with self.assertRaises(exceptions.NotFound):
                self.plugin.update_ip_address(self.context,
                                              'no_ip_address_id',
                                              {'ip_address': {'port_ids': []}})

    def test_update_ip_address_port_not_found(self):
        ip = dict(id=1, address=3232235876, address_readable="192.168.1.100",
                  subnet_id=1, network_id=2, version=4)
        with self._stubs(ports=[], addr=ip):
            with self.assertRaises(exceptions.NotFound):
                ip_address = {'ip_address': {'port_ids': ['fake']}}
                self.plugin.update_ip_address(self.context,
                                              ip["id"],
                                              ip_address)

    def test_update_ip_address_specify_ports(self):
        port = dict(id=1, network_id=2, ip_addresses=[])
        ip = dict(id=1, address=3232235876, address_readable="192.168.1.100",
                  subnet_id=1, network_id=2, version=4)
        with self._stubs(ports=[port], addr=ip):
            ip_address = {'ip_address': {'port_ids': [port['id']],
                                         'network_id': 2}}
            response = self.plugin.update_ip_address(self.context,
                                                     ip['id'],
                                                     ip_address)
            self.assertEqual(response['port_ids'], [port['id']])

    def _create_patch(self, path):
        patcher = patch(path)
        mocked = patcher.start()
        self.addCleanup(patcher.stop)
        return mocked

    def test_update_ip_address_update_deallocated_at(self):
        port = dict(id=1, network_id=2, ip_addresses=[])
        ip = dict(id=1, address=3232235876, address_readable="192.168.1.100",
                  subnet_id=1, network_id=2, version=4, deallocated=1,
                  deallocated_at='2020-01-01 00:00:00')

        path = 'quark.plugin_modules.ip_addresses'
        lookup = self._create_patch('%s._get_deallocated_override' % path)

        with self._stubs(ports=[port], addr=ip):
            ip_address = {'ip_address': {'reset_allocation_time': True}}
            self.plugin.update_ip_address(self.admin_context, ip['id'],
                                          ip_address)
            self.assertTrue(lookup.called)

    def test_update_ip_address_update_deallocated_at_not_deallocated(self):
        port = dict(id=1, network_id=2, ip_addresses=[])
        ip = dict(id=1, address=3232235876, address_readable="192.168.1.100",
                  subnet_id=1, network_id=2, version=4, deallocated=0,
                  deallocated_at='2020-01-01 00:00:00')

        path = 'quark.plugin_modules.ip_addresses'
        lookup = self._create_patch('%s._get_deallocated_override' % path)

        with self._stubs(ports=[port], addr=ip):
            ip_address = {'ip_address': {'reset_allocation_time': True}}
            self.plugin.update_ip_address(self.admin_context, ip['id'],
                                          ip_address)
            self.assertFalse(lookup.called)

    def test_update_ip_address_update_deallocated_at_not_admin(self):
        port = dict(id=1, network_id=2, ip_addresses=[])
        ip = dict(id=1, address=3232235876, address_readable="192.168.1.100",
                  subnet_id=1, network_id=2, version=4, deallocated=1,
                  deallocated_at='2020-01-01 00:00:00')

        with self._stubs(ports=[port], addr=ip):
            ip_address = {'ip_address': {'reset_allocation_time': True}}
            with self.assertRaises(webob.exc.HTTPForbidden):
                self.plugin.update_ip_address(self.context, ip['id'],
                                              ip_address)

    def test_update_ip_address_no_ports(self):
        port = dict(id=1, network_id=2, ip_addresses=[])
        ip = dict(id=1, address=3232235876, address_readable="192.168.1.100",
                  subnet_id=1, network_id=2, version=4)
        with self._stubs(ports=[port], addr=ip):
            ip_address = {'ip_address': {}}
            response = self.plugin.update_ip_address(self.context,
                                                     ip['id'],
                                                     ip_address)
            self.assertEqual(response['port_ids'], [])

    def test_update_ip_address_empty_ports_delete(self):
        port = dict(id=1, network_id=2, ip_addresses=[])
        ip = dict(id=1, address=3232235876, address_readable="192.168.1.100",
                  subnet_id=1, network_id=2, version=4)
        with self._stubs(ports=[port], addr=ip, addr_ports=True):
            ip_address = {'ip_address': {'port_ids': []}}
            response = self.plugin.update_ip_address(self.context,
                                                     ip['id'],
                                                     ip_address)
            self.assertEqual(response['port_ids'], [])


class TestQuarkUpdateIPAddressQuotaCheck(test_quark_plugin.TestQuarkPlugin):
    @contextlib.contextmanager
    def _stubs(self, port, addresses):
        port_models = []
        addr_model = None

        port_model = models.Port()
        port_model.update(port)
        port_models.append(port_model)

        for addr in addresses:
            addr_model = models.IPAddress()
            addr_model.update(addr)
            port_model["ip_addresses"].append(addr_model)

        db_mod = "quark.db.api"
        with contextlib.nested(
            mock.patch("%s.port_find" % db_mod),
            mock.patch("%s.ip_address_find" % db_mod),
            mock.patch("%s.port_associate_ip" % db_mod),
            mock.patch("%s.port_disassociate_ip" % db_mod),
            mock.patch("quark.plugin_modules.ip_addresses"
                       ".validate_ports_on_network_and_same_segment"),
            mock.patch("quark.plugin_modules.ip_addresses.ipam_driver")
        ) as (port_find, ip_find, port_associate_ip, port_disassociate_ip, val,
              mock_ipam):
            port_find.return_value = port_models
            ip_find.return_value = addr_model
            port_associate_ip.side_effect = _port_associate_stub
            port_disassociate_ip.side_effect = _port_disassociate_stub
            mock_ipam.deallocate_ip_address.side_effect = (
                _ip_deallocate_stub)
            yield

    def test_update_ip_address_port_over_quota(self):
        addresses = [{"id": ip, "address": ip} for ip in xrange(5)]

        port = dict(id=1, network_id=2)
        ip = dict(id=1, address=3232235876, address_readable="192.168.1.100",
                  subnet_id=1, network_id=2, version=4, deallocated=1,
                  deallocated_at='2020-01-01 00:00:00')

        with self._stubs(port=port, addresses=addresses):
            ip_address = {'ip_address': {"port_ids": [port]}}
            with self.assertRaises(exceptions.OverQuota):
                self.plugin.update_ip_address(self.admin_context, ip['id'],
                                              ip_address)


class TestQuarkGetIpAddress(test_quark_plugin.TestQuarkPlugin):
    @contextlib.contextmanager
    def _stubs(self, ips, ports):
        with mock.patch("quark.db.api.ip_address_find") as ip_find:
            port_models = []
            for port in ports:
                p = models.Port()
                p.update(port)
                port_models.append(p)
                if ips:
                    version = ips.pop("version")
                    ip_mod = models.IPAddress()
                    ip_mod.update(ips)
                    ip_mod.version = version
                    ip_mod.ports = port_models
                    # Set up Port to IP associations
                    assoc = models.PortIpAssociation()
                    assoc.port = p
                    assoc.port_id = p.id
                    assoc.ip_address = ip_mod
                    assoc.ip_address_id = ip_mod.id
                    ip_mod.associations.append(assoc)
                    ip_find.return_value = ip_mod
                else:
                    ip_find.return_value = ips
            yield

    def test_get_ip_address(self):
        port = dict(id=100)
        ip = dict(id=1, address=3232235876, address_readable="192.168.1.100",
                  subnet_id=1, network_id=2, version=4)
        with self._stubs(ips=ip, ports=[port]):
            res = self.plugin.get_ip_address(self.context, 1)
            self.assertEqual(ip["id"], res["id"])
            self.assertEqual(ip["subnet_id"], res["subnet_id"])
            self.assertEqual(ip["address_readable"], res["address"])
            self.assertEqual(res["port_ids"][0], port["id"])

    def test_get_ip_address_no_ip_fails(self):
        port = dict(id=100)
        with self._stubs(ips=None, ports=[port]):
            with self.assertRaises(quark_exceptions.IpAddressNotFound):
                self.plugin.get_ip_address(self.context, 1)


class TestQuarkGetIpAddresses(test_quark_plugin.TestQuarkPlugin):
    @contextlib.contextmanager
    def _stubs(self, ips, ports):
        with mock.patch("quark.db.api.ip_address_find") as ip_find:
            ip_models = []
            port_models = []
            for port in ports:
                p = models.Port()
                p.update(port)
                port_models.append(p)
                for ip in ips:
                    version = ip.pop("version")
                    ip_mod = models.IPAddress()
                    ip_mod.update(ip)
                    ip_mod.version = version
                    ip_mod.ports = port_models
                    # Set up Port to IP associations
                    assoc = models.PortIpAssociation()
                    assoc.port = p
                    assoc.port_id = p.id
                    assoc.ip_address = ip_mod
                    assoc.ip_address_id = ip_mod.id
                    ip_mod.associations.append(assoc)
                    ip_models.append(ip_mod)
                ip_find.return_value = ip_models
            yield

    def test_get_ip_addresses(self):
        port = dict(id=100, device_id="foobar")
        ip = dict(id=1, address=3232235876, address_readable="192.168.1.100",
                  subnet_id=1, network_id=2, version=4)
        with self._stubs(ips=[ip], ports=[port]):
            res = self.plugin.get_ip_addresses(self.context)
            addr_res = res[0]
            self.assertEqual(ip["id"], addr_res["id"])
            self.assertEqual(ip["subnet_id"], addr_res["subnet_id"])
            self.assertEqual(ip["address_readable"], addr_res["address"])
            self.assertEqual(addr_res["port_ids"][0], port["id"])

    def test_get_ip_addresses_multiple(self):
        port = dict(id=100, device_id="foobar")
        ips = [dict(id=1, address=3232235876, address_readable="192.168.1.100",
                    subnet_id=1, network_id=2, version=4),
               dict(id=2, address=3232235878, address_readable="192.168.1.101",
                    subnet_id=1, network_id=2, version=4)]
        with self._stubs(ips=ips, ports=[port]):
            res = self.plugin.get_ip_addresses(self.context)
            self.assertEqual(len(res), 2)
            for i, addr in enumerate(sorted(res, key=lambda x: x['id'])):
                self.assertEqual(ips[i]["id"], addr["id"])
                self.assertEqual(ips[i]["subnet_id"], addr["subnet_id"])
                self.assertEqual(ips[i]["address_readable"], addr["address"])


@mock.patch("quark.plugin_modules.ip_addresses"
            ".validate_ports_on_network_and_same_segment")
@mock.patch("quark.plugin_modules.ip_addresses.ipam_driver")
@mock.patch("quark.plugin_modules.ip_addresses.db_api")
class TestQuarkGetIpAddressPort(test_quark_plugin.TestQuarkPlugin):
    def _alloc_stub(self, ip_model):
        def _alloc_ip(context, addr, *args, **kwargs):
            addr.append(ip_model)
        return _alloc_ip

    def test_get_ip_address_ports(self, mock_dbapi, mock_ipam, *args):
        port = dict(mac_address="AA:BB:CC:DD:EE:FF", network_id=1,
                    tenant_id=self.context.tenant_id, device_id=2,
                    bridge="xenbr0", device_owner='network:dhcp',
                    service='none', id=100)
        ip = dict(id=1, address=3232235876, address_readable="192.168.1.100",
                  subnet_id=1, network_id=2, version=4)
        port_model = models.Port()
        port_model.update(port)
        ip_model = models.IPAddress()
        ip_model.update(ip)

        mock_dbapi.port_find.return_value = [port_model]
        mock_ipam.allocate_ip_address.side_effect = (
            self._alloc_stub(ip_model))

        res = self.plugin.get_ports_for_ip_address(self.context, 1)[0]
        mock_dbapi.port_find.assert_called_with(self.context, None, None, None,
                                                join_security_groups=True,
                                                fields=None,
                                                ip_address_id=[1])

        self.assertEqual(port["id"], res["id"])
        self.assertEqual(port["service"], res["service"])
        self.assertEqual(port["device_id"], res["device_id"])
        self.assertFalse('mac_address' in res)
        self.assertFalse('network_id' in res)
        self.assertFalse('bridge' in res)
        self.assertFalse('tenant_id' in res)

    def test_get_ip_address_port(self, mock_dbapi, mock_ipam, *args):
        port = dict(mac_address="AA:BB:CC:DD:EE:FF", network_id=1,
                    tenant_id=self.context.tenant_id, device_id=2,
                    bridge="xenbr0", device_owner='network:dhcp',
                    service='none', id=100)
        port2 = dict(mac_address="AA:BB:CC:DD:EE:FF", network_id=1,
                     tenant_id=self.context.tenant_id, device_id=2,
                     bridge="xenbr0", device_owner='network:dhcp',
                     service='none', id=100)
        ip = dict(id=1, address=3232235876, address_readable="192.168.1.100",
                  subnet_id=1, network_id=2, version=4)
        port_model = models.Port()
        port_model.update(port)
        port_model2 = models.Port()
        port_model2.update(port2)
        ip_model = models.IPAddress()
        ip_model.update(ip)

        mock_dbapi.port_find.return_value = port_model
        mock_ipam.allocate_ip_address.side_effect = (
            self._alloc_stub(ip_model))
        mock_dbapi.ip_address_find.return_value = ip_model

        res = self.plugin.get_port_for_ip_address(self.context, 1, 100)

        mock_dbapi.ip_address_find.assert_called_with(self.context,
                                                      scope=mock_dbapi.ONE,
                                                      id=1)
        mock_dbapi.port_find.assert_called_with(self.context, fields=None,
                                                id=100, ip_address_id=[1],
                                                scope=mock_dbapi.ONE)
        self.assertEqual(port["id"], res["id"])
        self.assertEqual(port["service"], res["service"])
        self.assertEqual(port["device_id"], res["device_id"])
        self.assertFalse('mac_address' in res)
        self.assertFalse('network_id' in res)
        self.assertFalse('bridge' in res)
        self.assertFalse('tenant_id' in res)

    def test_deleting_inactive_shared_ip(self, mock_dbapi, mock_ipam, *args):
        port = dict(id=100, service='none', network_id=2,
                    backend_key="derp", device_id="y")
        port2 = dict(id=101, service='none', network_id=2,
                     backend_key="derp", device_id="x")
        ip = dict(id=1, address=3232235876, address_readable="192.168.1.100",
                  subnet_id=1, network_id=2, version=4, address_type="shared")
        port_model = models.Port()
        port_model2 = models.Port()
        port_model.update(port)
        port_model2.update(port2)
        ip_model = models.IPAddress()
        ip_model.update(ip)
        ip_model.ports = [port_model, port_model2]

        mock_dbapi.port_find.return_value = port_model
        mock_dbapi.ip_address_find.return_value = ip_model
        mock_ipam.allocate_ip_address.side_effect = (
            self._alloc_stub(ip_model))
        self.plugin.delete_ip_address(self.context, 1)
        self.assertFalse(mock_dbapi.ip_address_delete.called)
        self.assertTrue(mock_ipam.deallocate_ip_address.called)

    def test_get_ip_address_no_ip_fails(self, mock_dbapi, mock_ipam, *args):
        mock_dbapi.ip_address_find.return_value = []
        with self.assertRaises(quark_exceptions.IpAddressNotFound):
            self.plugin.get_port_for_ip_address(self.context, 123, 100)

    def test_get_ip_address_no_port_fails(self, mock_dbapi, mock_ipam, *args):
        mock_dbapi.port_find.return_value = []
        with self.assertRaises(exceptions.PortNotFound):
            self.plugin.get_port_for_ip_address(self.context, 123, 100)

    def test_bad_request_when_deleting_active_shared_ip(self, mock_dbapi,
                                                        mock_ipam, *args):
        port = dict(id=100, service='considered_active', network_id=2,
                    backend_key="derp", device_id="y")
        port2 = dict(id=101, service='considered_active', network_id=2,
                     backend_key="derp", device_id="x")
        ip = dict(id=1, address=3232235876, address_readable="192.168.1.100",
                  subnet_id=1, network_id=2, version=4, address_type="shared")
        port_model = models.Port()
        port_model2 = models.Port()
        port_model.update(port)
        port_model2.update(port2)
        ip_model = models.IPAddress()
        ip_model.update(ip)
        ip_model.ports = [port_model, port_model2]

        mock_dbapi.port_find.return_value = port_model
        mock_dbapi.ip_address_find.return_value = ip_model
        mock_ipam.allocate_ip_address.side_effect = (
            self._alloc_stub(ip_model))
        qe = quark_exceptions
        with self.assertRaises(qe.PortRequiresDisassociation):
            self.plugin.delete_ip_address(self.context, 1)

    def test_bad_request_deleting_single_active_shared_ip(self, mock_dbapi,
                                                          mock_ipam, *args):
        port = dict(id=100, service='none', network_id=2,
                    backend_key="derp", device_id="y")
        port2 = dict(id=101, service='considered_active', network_id=2,
                     backend_key="derp", device_id="x")
        ip = dict(id=1, address=3232235876, address_readable="192.168.1.100",
                  subnet_id=1, network_id=2, version=4, address_type="shared")
        port_model = models.Port()
        port_model2 = models.Port()
        port_model.update(port)
        port_model2.update(port2)
        ip_model = models.IPAddress()
        ip_model.update(ip)
        ip_model.ports = [port_model, port_model2]

        mock_dbapi.port_find.return_value = port_model
        mock_dbapi.ip_address_find.return_value = ip_model
        mock_ipam.allocate_ip_address.side_effect = (
            self._alloc_stub(ip_model))
        qe = quark_exceptions
        with self.assertRaises(qe.PortRequiresDisassociation):
            self.plugin.delete_ip_address(self.context, 1)

    def test_update_port_service_inactive_ip(self, mock_dbapi, mock_ipam,
                                             *args):
        port = dict(id=100, service='none', network_id=2,
                    backend_key="derp", device_id="y")
        port2 = dict(id=101, service='none', network_id=2,
                     backend_key="derp", device_id="x")
        ip = dict(id=1, address=3232235876, address_readable="192.168.1.100",
                  subnet_id=1, network_id=2, version=4, address_type="shared")
        port_model = models.Port()
        port_model2 = models.Port()
        port_model.update(port)
        port_model2.update(port2)
        ip_model = models.IPAddress()
        ip_model.update(ip)
        ip_model.ports = [port_model, port_model2]

        mock_port_update = patch('quark.plugin_modules.ports.update_port')
        self.addCleanup(mock_port_update.stop)
        mock_port_update = mock_port_update.start()

        mock_dbapi.port_find.return_value = port_model
        mock_dbapi.ip_address_find.return_value = ip_model
        mock_ipam.allocate_ip_address.side_effect = (
            self._alloc_stub(ip_model))
        port_update = dict(service='derp')
        port_update = {'port': port_update}
        self.plugin.update_port_for_ip(self.context, 1, 100, port_update)
        self.assertTrue(mock_port_update.called)
        self.assertTrue(mock_port_update.called_once_with(
            self.context, 100, port_update))

    def test_update_shared_ip_deactivate(self, mock_dbapi, mock_ipam, *args):
        port = dict(id=100, service='compute', network_id=2,
                    backend_key="derp", device_id="y")
        port2 = dict(id=101, service='none', network_id=2,
                     backend_key="derp", device_id="x")
        ip = dict(id=1, address=3232235876, address_readable="192.168.1.100",
                  subnet_id=1, network_id=2, version=4, address_type="shared")
        port_model = models.Port()
        port_model2 = models.Port()
        port_model.update(port)
        port_model2.update(port2)
        ip_model = models.IPAddress()
        ip_model.update(ip)
        ip_model.ports = [port_model, port_model2]

        mock_port_update = patch('quark.plugin_modules.ports.update_port')
        self.addCleanup(mock_port_update.stop)
        mock_port_update = mock_port_update.start()

        mock_dbapi.port_find.return_value = port_model
        mock_dbapi.ip_address_find.return_value = ip_model
        mock_ipam.allocate_ip_address.side_effect = (
            self._alloc_stub(ip_model))
        port_update = dict(service='derp')
        port_update = {'port': port_update}
        self.plugin.update_port_for_ip(self.context, 1, 100, port_update)
        self.assertTrue(mock_port_update.called)
        self.assertTrue(mock_port_update.called_once_with(
            self.context, 100, port_update))
