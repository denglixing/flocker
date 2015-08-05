# Copyright Hybrid Logic Ltd.  See LICENSE file for details.

"""
Tests for ``flocker.control._persistence``.
"""

import json

from uuid import uuid4

from eliot.testing import validate_logging, assertHasMessage, assertHasAction

from twisted.internet import reactor
from twisted.trial.unittest import TestCase, SynchronousTestCase
from twisted.python.filepath import FilePath

from pyrsistent import PRecord

from .._persistence import (
    ConfigurationPersistenceService, wire_decode, wire_encode,
    _LOG_SAVE, _LOG_STARTUP, LeaseService, migrate_configuration,
    _CLASS_MARKER,
    )
from .._model import (
    Deployment, Application, DockerImage, Node, Dataset, Manifestation,
    AttachedVolume, SERIALIZABLE_CLASSES, NodeState)


DATASET = Dataset(dataset_id=unicode(uuid4()),
                  metadata={u"name": u"myapp"})
MANIFESTATION = Manifestation(dataset=DATASET, primary=True)
TEST_DEPLOYMENT = Deployment(
    nodes=[Node(uuid=uuid4(),
                applications=[
                    Application(
                        name=u'myapp',
                        image=DockerImage.from_string(u'postgresql:7.6'),
                        volume=AttachedVolume(
                            manifestation=MANIFESTATION,
                            mountpoint=FilePath(b"/xxx/yyy"))
                    )],
                manifestations={DATASET.dataset_id: MANIFESTATION})])


class FakePersistenceService(object):
    """
    A very simple fake persistence service that does nothing.
    """
    def __init__(self):
        self._deployment = Deployment(nodes=frozenset())

    def save(self, deployment):
        self._deployment = deployment

    def get(self):
        return self._deployment


class LeaseServiceTests(TestCase):
    """
    Tests for ``LeaseService``.
    """
    def service(self):
        """
        Start a lease service and schedule it to stop.

        :return: Started ``LeaseService``.
        """
        service = LeaseService(reactor, FakePersistenceService())
        service.startService()
        self.addCleanup(service.stopService)
        return service

    def test_expired_lease_removed(self):
        """
        A lease that has expired is removed from the persisted
        configuration.

        XXX Leases cannot be manipulated in this branch. See FLOC-2375.
        This is a skeletal test that merely ensures the call to
        ``update_leases`` takes place when ``_expire`` is called and should
        be rewritten to test the updated configuration once the configuration
        is aware of Leases.
        """
        service = self.service()
        d = service._expire()

        def check_expired(updated):
            self.assertIsNone(updated)

        d.addCallback(check_expired)
        return d


class ConfigurationPersistenceServiceTests(TestCase):
    """
    Tests for ``ConfigurationPersistenceService``.
    """
    def service(self, path, logger=None):
        """
        Start a service, schedule its stop.

        :param FilePath path: Where to store data.
        :param logger: Optional eliot ``Logger`` to set before startup.

        :return: Started ``ConfigurationPersistenceService``.
        """
        service = ConfigurationPersistenceService(reactor, path)
        if logger is not None:
            self.patch(service, "logger", logger)
        service.startService()
        self.addCleanup(service.stopService)
        return service

    def test_empty_on_start(self):
        """
        If no configuration was previously saved, starting a service results
        in an empty ``Deployment``.
        """
        service = self.service(FilePath(self.mktemp()))
        self.assertEqual(service.get(), Deployment(nodes=frozenset()))

    def test_directory_is_created(self):
        """
        If a directory does not exist in given path, it is created.
        """
        path = FilePath(self.mktemp())
        self.service(path)
        self.assertTrue(path.isdir())

    def test_file_is_created(self):
        """
        If no configuration file exists in the given path, it is created.
        """
        path = FilePath(self.mktemp())
        self.service(path)
        self.assertTrue(path.child(b"current_configuration.v1.json").exists())

    @validate_logging(assertHasAction, _LOG_SAVE, succeeded=True,
                      startFields=dict(configuration=TEST_DEPLOYMENT))
    def test_save_then_get(self, logger):
        """
        A configuration that was saved can subsequently retrieved.
        """
        service = self.service(FilePath(self.mktemp()), logger)
        d = service.save(TEST_DEPLOYMENT)
        d.addCallback(lambda _: service.get())
        d.addCallback(self.assertEqual, TEST_DEPLOYMENT)
        return d

    @validate_logging(assertHasMessage, _LOG_STARTUP,
                      fields=dict(configuration=TEST_DEPLOYMENT))
    def test_persist_across_restarts(self, logger):
        """
        A configuration that was saved can be loaded from a new service.
        """
        path = FilePath(self.mktemp())
        service = ConfigurationPersistenceService(reactor, path)
        service.startService()
        d = service.save(TEST_DEPLOYMENT)
        d.addCallback(lambda _: service.stopService())

        def retrieve_in_new_service(_):
            new_service = self.service(path, logger)
            self.assertEqual(new_service.get(), TEST_DEPLOYMENT)
        d.addCallback(retrieve_in_new_service)
        return d

    def test_register_for_callback(self):
        """
        Callbacks can be registered that are called every time there is a
        change saved.
        """
        service = self.service(FilePath(self.mktemp()))
        callbacks = []
        callbacks2 = []
        service.register(lambda: callbacks.append(1))
        d = service.save(TEST_DEPLOYMENT)

        def saved(_):
            service.register(lambda: callbacks2.append(1))
            return service.save(TEST_DEPLOYMENT)
        d.addCallback(saved)

        def saved_again(_):
            self.assertEqual((callbacks, callbacks2), ([1, 1], [1]))
        d.addCallback(saved_again)
        return d

    @validate_logging(
        lambda test, logger:
        test.assertEqual(len(logger.flush_tracebacks(ZeroDivisionError)), 1))
    def test_register_for_callback_failure(self, logger):
        """
        Failed callbacks don't prevent later callbacks from being called.
        """
        service = self.service(FilePath(self.mktemp()), logger)
        callbacks = []
        service.register(lambda: 1/0)
        service.register(lambda: callbacks.append(1))
        d = service.save(TEST_DEPLOYMENT)

        def saved(_):
            self.assertEqual(callbacks, [1])
        d.addCallback(saved)
        return d


class WireEncodeDecodeTests(SynchronousTestCase):
    """
    Tests for ``wire_encode`` and ``wire_decode``.
    """
    def test_encode_to_bytes(self):
        """
        ``wire_encode`` converts the given object to ``bytes``.
        """
        self.assertIsInstance(wire_encode(TEST_DEPLOYMENT), bytes)

    def test_roundtrip(self):
        """
        ``wire_decode`` returns object passed to ``wire_encode``.
        """
        self.assertEqual(TEST_DEPLOYMENT,
                         wire_decode(wire_encode(TEST_DEPLOYMENT)))

    def test_no_arbitrary_decoding(self):
        """
        ``wire_decode`` will not decode classes that are not in
        ``SERIALIZABLE_CLASSES``.
        """
        class Temp(PRecord):
            """A class."""
        SERIALIZABLE_CLASSES.append(Temp)

        def cleanup():
            if Temp in SERIALIZABLE_CLASSES:
                SERIALIZABLE_CLASSES.remove(Temp)
        self.addCleanup(cleanup)

        data = wire_encode(Temp())
        SERIALIZABLE_CLASSES.remove(Temp)
        # Possibly future versions might throw exception, the key point is
        # that the returned object is not a Temp instance.
        self.assertFalse(isinstance(wire_decode(data), Temp))

    def test_complex_keys(self):
        """
        Objects with attributes that are ``PMap``\s with complex keys
        (i.e. not strings) can be roundtripped.
        """
        node_state = NodeState(hostname=u'127.0.0.1', uuid=uuid4(),
                               manifestations={}, paths={},
                               devices={uuid4(): FilePath(b"/tmp")})
        self.assertEqual(node_state, wire_decode(wire_encode(node_state)))


class ConfigurationMigrationTests(SynchronousTestCase):
    """
    Tests for ``_ConfigurationMigration``.
    """
    configurations_dir = FilePath(__file__).sibling('configurations')

    def test_v0_v1_configuration(self):
        """
        A V0 JSON configuration blob is transformed to a V1 configuration
        blob, with the result validating when loaded.
        """
        v0_json = self.configurations_dir.child(
            'configuration_v0.json').getContent()
        v1_json = migrate_configuration(0, 1, v0_json)
        v1_config = wire_decode(v1_json)
        self.assertEqual(v1_config, Deployment(nodes=frozenset()))

    def test_v1_v0_configuration(self):
        """
        A V1 JSON configuration blob is transformed to a V0 configuration
        blob.
        """
        config_dict = {_CLASS_MARKER: u"Deployment"}
        v1_json = self.configurations_dir.child(
            'configuration_v1.json').getContent()
        v0_json = migrate_configuration(1, 0, v1_json)
        self.assertEqual(v0_json, json.dumps(config_dict))
