# Copyright 2017 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for the twisted metadata API."""

__all__ = []

import base64
import bz2
from datetime import datetime
from io import BytesIO
import json
from unittest.mock import (
    call,
    Mock,
    sentinel,
)

from crochet import wait_for
from maasserver.enum import NODE_STATUS
from maasserver.models import (
    Event,
    Tag,
)
from maasserver.models.signals.testing import SignalsDisabled
from maasserver.testing.factory import factory
from maasserver.testing.testcase import (
    MAASServerTestCase,
    MAASTransactionServerTestCase,
)
from maasserver.utils.orm import (
    reload_object,
    transactional,
    TransactionManagementError,
)
from maasserver.utils.threads import deferToDatabase
from maastesting.matchers import (
    MockCalledOnceWith,
    MockCallsMatch,
    MockNotCalled,
)
from maastesting.testcase import MAASTestCase
from metadataserver import api
from metadataserver.api_twisted import (
    StatusHandlerResource,
    StatusWorkerService,
)
from metadataserver.enum import SCRIPT_STATUS
from metadataserver.models import NodeKey
from testtools import ExpectedException
from testtools.matchers import (
    Equals,
    MatchesListwise,
    MatchesSetwise,
)
from twisted.internet.defer import (
    inlineCallbacks,
    succeed,
)
from twisted.web.server import NOT_DONE_YET
from twisted.web.test.requesthelper import DummyRequest


wait_for_reactor = wait_for(30)


class TestStatusHandlerResource(MAASTestCase):

    def make_request(self, content=None, token=None):
        request = DummyRequest([])
        if token is None:
            token = factory.make_name('token')
        request.requestHeaders.addRawHeader(
            b'authorization', 'oauth_token=%s' % token)
        if content is not None:
            request.content = BytesIO(content)
        return request

    def test__init__(self):
        resource = StatusHandlerResource(sentinel.status_worker)
        self.assertIs(sentinel.status_worker, resource.worker)
        self.assertTrue(resource.isLeaf)
        self.assertEquals([b'POST'], resource.allowedMethods)
        self.assertEquals(
            ['event_type', 'origin', 'name', 'description'],
            resource.requiredMessageKeys)

    def test__render_POST_missing_authorization(self):
        resource = StatusHandlerResource(sentinel.status_worker)
        request = DummyRequest([])
        output = resource.render_POST(request)
        self.assertEquals(b'', output)
        self.assertEquals(401, request.responseCode)

    def test__render_POST_empty_authorization(self):
        resource = StatusHandlerResource(sentinel.status_worker)
        request = DummyRequest([])
        request.requestHeaders.addRawHeader(b'authorization', '')
        output = resource.render_POST(request)
        self.assertEquals(b'', output)
        self.assertEquals(401, request.responseCode)

    def test__render_POST_bad_authorization(self):
        resource = StatusHandlerResource(sentinel.status_worker)
        request = DummyRequest([])
        request.requestHeaders.addRawHeader(
            b'authorization', factory.make_name('auth'))
        output = resource.render_POST(request)
        self.assertEquals(b'', output)
        self.assertEquals(401, request.responseCode)

    def test__render_POST_body_must_be_ascii(self):
        resource = StatusHandlerResource(sentinel.status_worker)
        request = self.make_request(content=b'\xe9')
        output = resource.render_POST(request)
        self.assertEquals(
            b"Status payload must be ASCII-only: 'ascii' codec can't "
            b"decode byte 0xe9 in position 0: ordinal not in range(128)",
            output)
        self.assertEquals(400, request.responseCode)

    def test__render_POST_body_must_be_valid_json(self):
        resource = StatusHandlerResource(sentinel.status_worker)
        request = self.make_request(content=b'testing not json')
        output = resource.render_POST(request)
        self.assertEquals(
            b"Status payload is not valid JSON:\ntesting not json\n\n",
            output)
        self.assertEquals(400, request.responseCode)

    def test__render_POST_validates_required_keys(self):
        resource = StatusHandlerResource(sentinel.status_worker)
        request = self.make_request(content=json.dumps({}).encode('ascii'))
        output = resource.render_POST(request)
        self.assertEquals(
            b'Missing parameter(s) event_type, origin, name, description '
            b'in status message.', output)
        self.assertEquals(400, request.responseCode)

    def test__render_POST_queue_messages(self):
        status_worker = Mock()
        status_worker.queueMessage = Mock()
        status_worker.queueMessage.return_value = succeed(None)
        resource = StatusHandlerResource(status_worker)
        message = {
            'event_type': (
                factory.make_name('type') + '/' +
                factory.make_name('sub_type')),
            'origin': factory.make_name('origin'),
            'name': factory.make_name('name'),
            'description': factory.make_name('description'),
        }
        token = factory.make_name('token')
        request = self.make_request(
            content=json.dumps(message).encode('ascii'), token=token)
        output = resource.render_POST(request)
        self.assertEquals(NOT_DONE_YET, output)
        self.assertEquals(204, request.responseCode)
        self.assertThat(
            status_worker.queueMessage, MockCalledOnceWith(token, message))


class TestStatusWorkerServiceTransactional(MAASTransactionServerTestCase):

    @transactional
    def make_nodes_with_tokens(self):
        nodes = [
            factory.make_Node()
            for _ in range(3)
        ]
        return [
            (node, NodeKey.objects.get_token_for_node(node))
            for node in nodes
        ]

    def make_message(self):
        return {
            'event_type': factory.make_name('type'),
            'origin': factory.make_name('origin'),
            'name': factory.make_name('name'),
            'description': factory.make_name('description'),
            'timestamp': datetime.utcnow().timestamp(),
        }

    def test__init__(self):
        worker = StatusWorkerService(sentinel.dbtasks, clock=sentinel.reactor)
        self.assertEqual(sentinel.dbtasks, worker.dbtasks)
        self.assertEqual(sentinel.reactor, worker.clock)
        self.assertEqual(60, worker.step)
        self.assertEqual((worker._tryUpdateNodes, tuple(), {}), worker.call)

    def test__tryUpdateNodes_returns_None_when_empty_queue(self):
        worker = StatusWorkerService(sentinel.dbtasks)
        self.assertIsNone(worker._tryUpdateNodes())

    @wait_for_reactor
    @inlineCallbacks
    def test__tryUpdateNodes_sends_work_to_dbtasks(self):
        nodes_with_tokens = yield deferToDatabase(self.make_nodes_with_tokens)
        node_messages = {
            node: [
                self.make_message()
                for _ in range(3)
            ]
            for node, _ in nodes_with_tokens
        }
        dbtasks = Mock()
        dbtasks.addTask = Mock()
        worker = StatusWorkerService(dbtasks)
        for node, token in nodes_with_tokens:
            for message in node_messages[node]:
                worker.queueMessage(token.key, message)
        yield worker._tryUpdateNodes()
        call_args = [
            (call_arg[0][1], call_arg[0][2])
            for call_arg in dbtasks.addTask.call_args_list
        ]
        self.assertThat(call_args, MatchesSetwise(*[
            MatchesListwise([Equals(node), Equals(messages)])
            for node, messages in node_messages.items()
        ]))

    @wait_for_reactor
    @inlineCallbacks
    def test__processMessages_fails_when_in_transaction(self):
        worker = StatusWorkerService(sentinel.dbtasks)
        with ExpectedException(TransactionManagementError):
            yield deferToDatabase(
                transactional(worker._processMessages),
                sentinel.node, [sentinel.message])

    @wait_for_reactor
    @inlineCallbacks
    def test__processMessageNow_fails_when_in_transaction(self):
        worker = StatusWorkerService(sentinel.dbtasks)
        with ExpectedException(TransactionManagementError):
            yield deferToDatabase(
                transactional(worker._processMessageNow),
                sentinel.node, sentinel.message)

    @wait_for_reactor
    @inlineCallbacks
    def test__processMessages_calls_processMessage_and_updateLastPing(self):
        worker = StatusWorkerService(sentinel.dbtasks)
        mock_processMessage = self.patch(worker, "_processMessage")
        mock_updateLastPing = self.patch(worker, "_updateLastPing")
        yield deferToDatabase(
            worker._processMessages, sentinel.node,
            [sentinel.message1, sentinel.message2])
        self.assertThat(
            mock_processMessage,
            MockCallsMatch(
                call(sentinel.node, sentinel.message1),
                call(sentinel.node, sentinel.message2)))
        self.assertThat(
            mock_updateLastPing,
            MockCalledOnceWith(sentinel.node, sentinel.message2))

    @wait_for_reactor
    @inlineCallbacks
    def test_queueMessages_processes_top_level_message_instantly(self):
        worker = StatusWorkerService(sentinel.dbtasks)
        mock_processMessage = self.patch(worker, "_processMessage")
        mock_updateLastPing = self.patch(worker, "_updateLastPing")
        message = self.make_message()
        message['event_type'] = 'finish'
        nodes_with_tokens = yield deferToDatabase(self.make_nodes_with_tokens)
        node, token = nodes_with_tokens[0]
        yield worker.queueMessage(token.key, message)
        self.assertThat(
            mock_processMessage,
            MockCalledOnceWith(node, message))
        self.assertThat(
            mock_updateLastPing,
            MockCalledOnceWith(node, message))

    @wait_for_reactor
    @inlineCallbacks
    def test_queueMessages_processes_files_message_instantly(self):
        worker = StatusWorkerService(sentinel.dbtasks)
        mock_processMessage = self.patch(worker, "_processMessage")
        contents = b'These are the contents of the file.'
        encoded_content = encode_as_base64(bz2.compress(contents))
        message = self.make_message()
        message['files'] = [
            {
                "path": "sample.txt",
                "encoding": "uuencode",
                "compression": "bzip2",
                "content": encoded_content
            }
        ]
        nodes_with_tokens = yield deferToDatabase(self.make_nodes_with_tokens)
        node, token = nodes_with_tokens[0]
        yield worker.queueMessage(token.key, message)
        self.assertThat(
            mock_processMessage,
            MockCalledOnceWith(node, message))

    @wait_for_reactor
    @inlineCallbacks
    def test_queueMessages_handled_invalid_nodekey_with_instant_msg(self):
        worker = StatusWorkerService(sentinel.dbtasks)
        mock_processMessage = self.patch(worker, "_processMessage")
        contents = b'These are the contents of the file.'
        encoded_content = encode_as_base64(bz2.compress(contents))
        message = self.make_message()
        message['files'] = [
            {
                "path": "sample.txt",
                "encoding": "uuencode",
                "compression": "bzip2",
                "content": encoded_content
            }
        ]
        nodes_with_tokens = yield deferToDatabase(self.make_nodes_with_tokens)
        node, token = nodes_with_tokens[0]
        yield deferToDatabase(token.delete)
        yield worker.queueMessage(token.key, message)
        self.assertThat(
            mock_processMessage, MockNotCalled())


def encode_as_base64(content):
    return base64.encodebytes(content).decode("ascii")


class TestStatusWorkerService(MAASServerTestCase):

    def setUp(self):
        super(TestStatusWorkerService, self).setUp()
        self.useFixture(SignalsDisabled("power"))

    def processMessage(self, node, payload):
        worker = StatusWorkerService(sentinel.dbtasks)
        worker._processMessage(node, payload)

    def updateLastPing(self, node, payload):
        worker = StatusWorkerService(sentinel.dbtasks)
        worker._updateLastPing(node, payload)

    def test_status_installation_result_does_not_affect_other_node(self):
        node1 = factory.make_Node(status=NODE_STATUS.DEPLOYING)
        node2 = factory.make_Node(status=NODE_STATUS.DEPLOYING)
        payload = {
            'event_type': 'finish',
            'result': 'SUCCESS',
            'origin': 'curtin',
            'name': 'cmd-install',
            'description': 'Command Install',
            'timestamp': datetime.utcnow(),
        }
        self.processMessage(node1, payload)
        self.assertEqual(
            NODE_STATUS.DEPLOYING, reload_object(node2).status)
        # Check last node1 event.
        self.assertEqual(
            "'curtin' Command Install",
            Event.objects.filter(node=node1).last().description)
        # There must me no events for node2.
        self.assertFalse(Event.objects.filter(node=node2).exists())

    def test_status_installation_success_leaves_node_deploying(self):
        node = factory.make_Node(interface=True, status=NODE_STATUS.DEPLOYING)
        payload = {
            'event_type': 'finish',
            'result': 'SUCCESS',
            'origin': 'curtin',
            'name': 'cmd-install',
            'description': 'Command Install',
            'timestamp': datetime.utcnow(),
        }
        self.processMessage(node, payload)
        self.assertEqual(NODE_STATUS.DEPLOYING, reload_object(node).status)
        # Check last node event.
        self.assertEqual(
            "'curtin' Command Install",
            Event.objects.filter(node=node).last().description)

    def test_status_commissioning_failure_leaves_node_failed(self):
        node = factory.make_Node(
            interface=True, status=NODE_STATUS.COMMISSIONING)
        payload = {
            'event_type': 'finish',
            'result': 'FAILURE',
            'origin': 'curtin',
            'name': 'commissioning',
            'description': 'Commissioning',
            'timestamp': datetime.utcnow(),
        }
        self.processMessage(node, payload)
        self.assertEqual(
            NODE_STATUS.FAILED_COMMISSIONING, reload_object(node).status)
        # Check last node event.
        self.assertEqual(
            "'curtin' Commissioning",
            Event.objects.filter(node=node).last().description)

    def test_status_commissioning_failure_clears_owner(self):
        user = factory.make_User()
        node = factory.make_Node(
            interface=True, status=NODE_STATUS.COMMISSIONING, owner=user)
        payload = {
            'event_type': 'finish',
            'result': 'FAILURE',
            'origin': 'curtin',
            'name': 'commissioning',
            'description': 'Commissioning',
            'timestamp': datetime.utcnow(),
        }
        self.assertEqual(user, node.owner)  # Node has an owner
        self.processMessage(node, payload)
        self.assertEqual(
            NODE_STATUS.FAILED_COMMISSIONING, reload_object(node).status)
        self.assertIsNone(reload_object(node).owner)

    def test_status_installation_failure_leaves_node_failed(self):
        node = factory.make_Node(interface=True, status=NODE_STATUS.DEPLOYING)
        payload = {
            'event_type': 'finish',
            'result': 'FAILURE',
            'origin': 'curtin',
            'name': 'cmd-install',
            'description': 'Command Install',
            'timestamp': datetime.utcnow(),
        }
        self.processMessage(node, payload)
        self.assertEqual(
            NODE_STATUS.FAILED_DEPLOYMENT, reload_object(node).status)
        # Check last node event.
        self.assertEqual(
            "Installation failed (refer to the installation"
            " log for more information).",
            Event.objects.filter(node=node).last().description)

    def test_status_installation_fail_leaves_node_failed(self):
        node = factory.make_Node(interface=True, status=NODE_STATUS.DEPLOYING)
        payload = {
            'event_type': 'finish',
            'result': 'FAIL',
            'origin': 'curtin',
            'name': 'cmd-install',
            'description': 'Command Install',
            'timestamp': datetime.utcnow(),
        }
        self.processMessage(node, payload)
        self.assertEqual(
            NODE_STATUS.FAILED_DEPLOYMENT, reload_object(node).status)
        # Check last node event.
        self.assertEqual(
            "Installation failed (refer to the installation"
            " log for more information).",
            Event.objects.filter(node=node).last().description)

    def test_status_installation_failure_doesnt_clear_owner(self):
        user = factory.make_User()
        node = factory.make_Node(
            interface=True, status=NODE_STATUS.DEPLOYING, owner=user)
        payload = {
            'event_type': 'finish',
            'result': 'FAILURE',
            'origin': 'curtin',
            'name': 'cmd-install',
            'description': 'Command Install',
            'timestamp': datetime.utcnow(),
        }
        self.assertEqual(user, node.owner)  # Node has an owner
        self.processMessage(node, payload)
        self.assertEqual(
            NODE_STATUS.FAILED_DEPLOYMENT, reload_object(node).status)
        self.assertIsNotNone(reload_object(node).owner)

    def test_status_commissioning_failure_does_not_populate_tags(self):
        populate_tags_for_single_node = self.patch(
            api, "populate_tags_for_single_node")
        node = factory.make_Node(
            interface=True, status=NODE_STATUS.COMMISSIONING)
        payload = {
            'event_type': 'finish',
            'result': 'FAILURE',
            'origin': 'curtin',
            'name': 'commissioning',
            'description': 'Commissioning',
            'timestamp': datetime.utcnow(),
        }
        self.processMessage(node, payload)
        self.assertEqual(
            NODE_STATUS.FAILED_COMMISSIONING, reload_object(node).status)
        self.assertThat(populate_tags_for_single_node, MockNotCalled())

    def test_status_erasure_failure_leaves_node_failed(self):
        node = factory.make_Node(
            interface=True, status=NODE_STATUS.DISK_ERASING)
        payload = {
            'event_type': 'finish',
            'result': 'FAILURE',
            'origin': 'curtin',
            'name': 'cmd-erase',
            'description': 'Erasing disk',
            'timestamp': datetime.utcnow(),
        }
        self.processMessage(node, payload)
        self.assertEqual(
            NODE_STATUS.FAILED_DISK_ERASING, reload_object(node).status)
        # Check last node event.
        self.assertEqual(
            "Failed to erase disks.",
            Event.objects.filter(node=node).last().description)

    def test_status_erasure_failure_does_not_populate_tags(self):
        populate_tags_for_single_node = self.patch(
            api, "populate_tags_for_single_node")
        node = factory.make_Node(
            interface=True, status=NODE_STATUS.DISK_ERASING)
        payload = {
            'event_type': 'finish',
            'result': 'FAILURE',
            'origin': 'curtin',
            'name': 'cmd-erase',
            'description': 'Erasing disk',
            'timestamp': datetime.utcnow(),
        }
        self.processMessage(node, payload)
        self.assertEqual(
            NODE_STATUS.FAILED_DISK_ERASING, reload_object(node).status)
        self.assertThat(populate_tags_for_single_node, MockNotCalled())

    def test_status_erasure_failure_doesnt_clear_owner(self):
        user = factory.make_User()
        node = factory.make_Node(
            interface=True, status=NODE_STATUS.DISK_ERASING, owner=user)
        payload = {
            'event_type': 'finish',
            'result': 'FAILURE',
            'origin': 'curtin',
            'name': 'cmd-erase',
            'description': 'Erasing disk',
            'timestamp': datetime.utcnow(),
        }
        self.processMessage(node, payload)
        self.assertEqual(
            NODE_STATUS.FAILED_DISK_ERASING, reload_object(node).status)
        self.assertEqual(user, node.owner)

    def test_status_with_file_bad_encoder_fails(self):
        node = factory.make_Node(
            interface=True, status=NODE_STATUS.COMMISSIONING)
        contents = b'These are the contents of the file.'
        encoded_content = encode_as_base64(bz2.compress(contents))
        payload = {
            'event_type': 'finish',
            'result': 'FAILURE',
            'origin': 'curtin',
            'name': 'commissioning',
            'description': 'Commissioning',
            'timestamp': datetime.utcnow(),
            'files': [
                {
                    "path": "sample.txt",
                    "encoding": "uuencode",
                    "compression": "bzip2",
                    "content": encoded_content
                }
            ]
        }
        with ExpectedException(ValueError):
            self.processMessage(node, payload)

    def test_status_with_file_bad_compression_fails(self):
        node = factory.make_Node(
            interface=True, status=NODE_STATUS.COMMISSIONING)
        contents = b'These are the contents of the file.'
        encoded_content = encode_as_base64(bz2.compress(contents))
        payload = {
            'event_type': 'finish',
            'result': 'FAILURE',
            'origin': 'curtin',
            'name': 'commissioning',
            'description': 'Commissioning',
            'timestamp': datetime.utcnow(),
            'files': [
                {
                    "path": "sample.txt",
                    "encoding": "base64",
                    "compression": "jpeg",
                    "content": encoded_content
                }
            ]
        }
        with ExpectedException(ValueError):
            self.processMessage(node, payload)

    def test_status_with_file_no_compression_succeeds(self):
        node = factory.make_Node(
            interface=True, status=NODE_STATUS.COMMISSIONING,
            with_empty_script_sets=True)
        script_result = (
            node.current_commissioning_script_set.scriptresult_set.first())
        script_result.status = SCRIPT_STATUS.RUNNING
        script_result.save()
        contents = b'These are the contents of the file.'
        encoded_content = encode_as_base64(contents)
        payload = {
            'event_type': 'finish',
            'result': 'FAILURE',
            'origin': 'curtin',
            'name': 'commissioning',
            'description': 'Commissioning',
            'timestamp': datetime.utcnow(),
            'files': [
                {
                    "path": script_result.name,
                    "encoding": "base64",
                    "content": encoded_content
                }
            ]
        }
        self.processMessage(node, payload)
        self.assertEqual(contents, reload_object(script_result).output)

    def test_status_with_file_invalid_statuses_fails(self):
        """Adding files should fail for every status that's neither
        COMMISSIONING nor DEPLOYING"""
        for node_status in [
                NODE_STATUS.DEFAULT,
                NODE_STATUS.NEW,
                NODE_STATUS.MISSING,
                NODE_STATUS.READY,
                NODE_STATUS.RESERVED,
                NODE_STATUS.RETIRED,
                NODE_STATUS.BROKEN,
                NODE_STATUS.ALLOCATED,
                NODE_STATUS.RELEASING,
                NODE_STATUS.FAILED_RELEASING,
                NODE_STATUS.DISK_ERASING,
                NODE_STATUS.FAILED_DISK_ERASING]:
            node = factory.make_Node(interface=True, status=node_status)
            contents = b'These are the contents of the file.'
            encoded_content = encode_as_base64(bz2.compress(contents))
            payload = {
                'event_type': 'finish',
                'result': 'FAILURE',
                'origin': 'curtin',
                'name': 'commissioning',
                'description': 'Commissioning',
                'timestamp': datetime.utcnow(),
                'files': [
                    {
                        "path": "sample.txt",
                        "encoding": "base64",
                        "compression": "bzip2",
                        "content": encoded_content
                    }
                ]
            }
            with ExpectedException(ValueError):
                self.processMessage(node, payload)

    def test_status_with_file_succeeds(self):
        """Adding files should succeed for every status that's either
        COMMISSIONING or DEPLOYING"""
        for node_status, target_status in [
                (NODE_STATUS.COMMISSIONING, NODE_STATUS.FAILED_COMMISSIONING),
                (NODE_STATUS.DEPLOYING, NODE_STATUS.FAILED_DEPLOYMENT)]:
            node = factory.make_Node(
                interface=True, status=node_status,
                with_empty_script_sets=True)
            if node_status == NODE_STATUS.COMMISSIONING:
                script_set = node.current_commissioning_script_set
            elif node_status == NODE_STATUS.DEPLOYING:
                script_set = node.current_installation_script_set
            script_result = script_set.scriptresult_set.first()
            script_result.status = SCRIPT_STATUS.RUNNING
            script_result.save()
            contents = b'These are the contents of the file.'
            encoded_content = encode_as_base64(bz2.compress(contents))
            payload = {
                'event_type': 'finish',
                'result': 'FAILURE',
                'origin': 'curtin',
                'name': 'commissioning',
                'description': 'Commissioning',
                'timestamp': datetime.utcnow(),
                'files': [
                    {
                        "path": script_result.name,
                        "encoding": "base64",
                        "compression": "bzip2",
                        "content": encoded_content
                    }
                ]
            }
            self.processMessage(node, payload)
            self.assertEqual(
                target_status, reload_object(node).status)
            # Check the node result.
            self.assertEqual(contents, reload_object(script_result).output)

    def test_status_with_results_succeeds(self):
        """Adding a script result should succeed"""
        node = factory.make_Node(
            interface=True, status=NODE_STATUS.COMMISSIONING,
            with_empty_script_sets=True)
        script_result = (
            node.current_commissioning_script_set.scriptresult_set.first())
        script_result.status = SCRIPT_STATUS.RUNNING
        script_result.save()
        contents = b'These are the contents of the file.'
        encoded_content = encode_as_base64(bz2.compress(contents))
        payload = {
            'event_type': 'finish',
            'result': 'FAILURE',
            'origin': 'curtin',
            'name': 'commissioning',
            'description': 'Commissioning',
            'timestamp': datetime.utcnow(),
            'files': [
                {
                    "path": script_result.name,
                    "encoding": "base64",
                    "compression": "bzip2",
                    "content": encoded_content,
                    "result": -42
                }
            ]
        }
        self.processMessage(node, payload)
        script_result = reload_object(script_result)
        self.assertEqual(contents, script_result.output)
        self.assertEqual(-42, script_result.exit_status)

    def test_status_with_results_no_exit_status_defaults_to_zero(self):
        """Adding a script result should succeed without a return code defaults
        it to zero."""
        node = factory.make_Node(
            interface=True, status=NODE_STATUS.COMMISSIONING,
            with_empty_script_sets=True)
        script_result = (
            node.current_commissioning_script_set.scriptresult_set.first())
        script_result.status = SCRIPT_STATUS.RUNNING
        script_result.save()
        contents = b'These are the contents of the file.'
        encoded_content = encode_as_base64(bz2.compress(contents))
        payload = {
            'event_type': 'finish',
            'result': 'FAILURE',
            'origin': 'curtin',
            'name': 'commissioning',
            'description': 'Commissioning',
            'timestamp': datetime.utcnow(),
            'files': [
                {
                    "path": script_result.name,
                    "encoding": "base64",
                    "compression": "bzip2",
                    "content": encoded_content,
                }
            ]
        }
        self.processMessage(node, payload)
        self.assertEqual(0, reload_object(script_result).exit_status)

    def test_status_stores_virtual_tag_on_node_if_virtual(self):
        node = factory.make_Node(
            status=NODE_STATUS.COMMISSIONING, with_empty_script_sets=True)
        content = 'virtual'.encode('utf-8')
        payload = {
            'event_type': 'finish',
            'result': 'SUCCESS',
            'origin': 'curtin',
            'name': 'commissioning',
            'description': 'Commissioning',
            'timestamp': datetime.utcnow(),
            'files': [
                {
                    "path": "00-maas-02-virtuality.out",
                    "encoding": "base64",
                    "content": encode_as_base64(content),
                }
            ]
        }
        self.processMessage(node, payload)
        node = reload_object(node)
        self.assertEqual(
            ["virtual"], [each_tag.name for each_tag in node.tags.all()])
        for script_result in node.current_commissioning_script_set:
            if script_result.name == "00-maas-02-virtuality":
                break
        self.assertEqual(content, script_result.stdout)

    def test_status_removes_virtual_tag_on_node_if_not_virtual(self):
        node = factory.make_Node(
            status=NODE_STATUS.COMMISSIONING, with_empty_script_sets=True)
        tag, _ = Tag.objects.get_or_create(name='virtual')
        node.tags.add(tag)
        content = 'none'.encode('utf-8')
        payload = {
            'event_type': 'finish',
            'result': 'SUCCESS',
            'origin': 'curtin',
            'name': 'commissioning',
            'description': 'Commissioning',
            'timestamp': datetime.utcnow(),
            'files': [
                {
                    "path": "00-maas-02-virtuality.out",
                    "encoding": "base64",
                    "content": encode_as_base64(content),
                }
            ]
        }
        self.processMessage(node, payload)
        node = reload_object(node)
        self.assertEqual(
            [], [each_tag.name for each_tag in node.tags.all()])
        for script_result in node.current_commissioning_script_set:
            if script_result.name == "00-maas-02-virtuality":
                break
        self.assertEqual(content, script_result.stdout)

    def test_updateLastPing_updates_script_status_last_ping(self):
        nodes = {
            status: factory.make_Node(
                status=status, with_empty_script_sets=True)
            for status in (
                NODE_STATUS.COMMISSIONING,
                NODE_STATUS.TESTING,
                NODE_STATUS.DEPLOYING)
        }

        for status, node in nodes.items():
            payload = {
                'event_type': 'progress',
                'origin': 'curtin',
                'name': 'test',
                'description': 'testing',
                'timestamp': datetime.utcnow(),
            }
            self.updateLastPing(node, payload)
            script_set_statuses = {
                NODE_STATUS.COMMISSIONING: (
                    node.current_commissioning_script_set),
                NODE_STATUS.TESTING: node.current_testing_script_set,
                NODE_STATUS.DEPLOYING: node.current_installation_script_set,
            }
            script_set = script_set_statuses.get(node.status)
            self.assertIsNotNone(script_set.last_ping)
