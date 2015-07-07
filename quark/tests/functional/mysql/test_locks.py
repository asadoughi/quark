import netaddr

from quark.db import api as db_api
from quark.tests.functional.mysql.base import MySqlBaseFunctionalTest


class QuarkLocks(MySqlBaseFunctionalTest):
    def test_create_lock(self):
        kwargs = {"address": netaddr.IPAddress("192.168.2.1")}
        ip_address = db_api.ip_address_create(self.context, **kwargs)
        kwargs = {"type": "ip_address", "name": "because i said so"}
        lock = db_api.lock_create(self.context, ip_address, **kwargs)

        ip_address = db_api.ip_address_find(self.context,
                                            id=ip_address["id"],
                                            scope=db_api.ONE)
        self.assertEqual(ip_address.lock_id, lock.id)

    def test_delete_lock(self):
        kwargs = {"address": netaddr.IPAddress("192.168.2.1")}
        ip_address = db_api.ip_address_create(self.context, **kwargs)
        kwargs = {"type": "ip_address", "name": "because i said so"}
        lock = db_api.lock_create(self.context, ip_address, **kwargs)
        ip_address = db_api.ip_address_find(self.context,
                                            id=ip_address["id"],
                                            scope=db_api.ONE)
        self.assertEqual(ip_address.lock_id, lock.id)

        db_api.lock_delete(self.context, ip_address, lock)
        self.assertIsNone(ip_address.lock_id)
