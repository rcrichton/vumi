"""Tests for vumi.application.rapidsms_relay."""

import json

from twisted.internet.defer import inlineCallbacks, returnValue
from twisted.web import http
from twisted.web.resource import Resource

from vumi.application.tests.test_base import ApplicationTestCase
from vumi.tests.utils import TestResourceWorker, LogCatcher, get_stubbed_worker
from vumi.application.rapidsms_relay import RapidSMSRelay, BadRequestError
from vumi.utils import http_request_full, basic_auth_string
from vumi.message import TransportUserMessage, from_json


class DummyRapidResource(Resource):
    isLeaf = True

    def __init__(self, callback):
        self.callback = callback

    def render_POST(self, request):
        return self.callback(request)


class RapidSMSRelayTestCase(ApplicationTestCase):

    application_class = RapidSMSRelay
    path = '/test/resource/path'

    @inlineCallbacks
    def setup_resource(self, callback=None, auth=None):
        if callback is None:
            callback = lambda r: self.fail("No RapidSMS requests expected.")
        self.resource = yield self.setup_dummy_rapidsms(callback=callback)
        self.app = yield self.setup_app(self.path, self.resource, auth=auth)

    @inlineCallbacks
    def setup_app(self, path, resource, auth=None):
        vumi_username, vumi_password = auth if auth else (None, None)
        app = yield self.get_application({
            'rapidsms_url': 'http://localhost:%s%s' % (resource.port, path),
            'web_path': '/send/',
            'web_port': '0',
            'rapidsms_username': 'username',
            'rapidsms_password': 'password',
            'vumi_username': vumi_username,
            'vumi_password': vumi_password,
        })
        returnValue(app)

    @inlineCallbacks
    def setup_dummy_rapidsms(self, callback):
        w = get_stubbed_worker(TestResourceWorker, {})
        w.set_resources([(self.path, DummyRapidResource, (callback,))])
        self._workers.append(w)
        yield w.startWorker()
        returnValue(w)

    def get_response_msgs(self, response):
        payloads = from_json(response.delivered_body)
        return [TransportUserMessage(_process_fields=False, **payload)
                for payload in payloads]

    @inlineCallbacks
    def test_rapidsms_relay_success(self):
        def cb(request):
            msg = TransportUserMessage.from_json(request.content.read())
            self.assertEqual(msg['content'], 'hello world')
            self.assertEqual(msg['from_addr'], '+41791234567')
            return 'OK'

        yield self.setup_resource(cb)
        yield self.dispatch(self.mkmsg_in())
        self.assertEqual([], self.get_dispatched_messages())

    @inlineCallbacks
    def test_rapidsms_relay_unicode(self):
        def cb(request):
            msg = TransportUserMessage.from_json(request.content.read())
            self.assertEqual(msg['content'], u'h\xc6llo')
            return 'OK'

        yield self.setup_resource(cb)
        yield self.dispatch(self.mkmsg_in(content=u'h\xc6llo'))
        self.assertEqual([], self.get_dispatched_messages())

    @inlineCallbacks
    def test_rapidsms_relay_with_basic_auth(self):
        def cb(request):
            self.assertEqual(request.getUser(), 'username')
            self.assertEqual(request.getPassword(), 'password')
            msg = TransportUserMessage.from_json(request.content.read())
            self.assertEqual(msg['message_id'], 'abc')
            self.assertEqual(msg['content'], 'hello world')
            self.assertEqual(msg['from_addr'], '+41791234567')
            return 'OK'

        yield self.setup_resource(cb)
        yield self.dispatch(self.mkmsg_in())
        self.assertEqual([], self.get_dispatched_messages())

    @inlineCallbacks
    def test_rapidsms_relay_with_bad_basic_auth(self):
        def cb(request):
            request.setResponseCode(http.UNAUTHORIZED)
            return 'Not Authorized'

        yield self.setup_resource(cb)
        yield self.dispatch(self.mkmsg_in())
        self.assertEqual([], self.get_dispatched_messages())

    @inlineCallbacks
    def test_rapidsms_relay_logs_events(self):
        yield self.setup_resource()
        with LogCatcher() as lc:
            yield self.dispatch(self.mkmsg_delivery(), rkey=self.rkey('event'))
            yield self.dispatch(self.mkmsg_ack(), rkey=self.rkey('event'))
            self.assertEqual(lc.messages(), [
                "Delivery report received for message u'abc',"
                " status u'delivered'",
                "Acknowledgement received for message u'1'",
            ])
        self.assertEqual([], self.get_dispatched_messages())

    def _call_relay(self, data, auth=None):
        data = json.dumps(data)
        host = self.app.web_resource.getHost()
        send_url = "http://localhost:%d/send" % (host.port,)
        headers = {}
        if auth is not None:
            headers['Authorization'] = basic_auth_string(*auth)
        return http_request_full(send_url, data, headers=headers)

    def _check_messages(self, response, expecteds):
        response_msgs = self.get_response_msgs(response)
        msgs = self.get_dispatched_messages()
        for rmsg, msg, expected in zip(response_msgs, msgs, expecteds):
            self.assertEqual(msg, rmsg)
            for k, v in expected.items():
                self.assertEqual(msg[k], v)
        self.assertEqual(len(msgs), len(expecteds))
        self.assertEqual(len(response_msgs), len(expecteds))

    @inlineCallbacks
    def test_rapidsms_relay_outbound(self):
        yield self.setup_resource()
        response = yield self._call_relay({
            'to_addr': ['+123456'],
            'content': 'foo',
        })
        self.assertEqual(response.headers.getRawHeaders('content-type'),
                         ['application/json; charset=utf-8'])
        self._check_messages(response, [
            {'to_addr': '+123456', 'content':  u'foo'}])

    @inlineCallbacks
    def test_rapidsms_relay_outbound_unicode(self):
        yield self.setup_resource()
        response = yield self._call_relay({
            'to_addr': ['+123456'],
            'content': u'f\xc6r',
        })
        self._check_messages(response, [
            {'to_addr': '+123456', 'content':  u'f\xc6r'}])

    @inlineCallbacks
    def test_rapidsms_relay_multiple_outbound(self):
        yield self.setup_resource()
        addresses = ['+123456', '+678901']
        response = yield self._call_relay({
            'to_addr': addresses,
            'content': 'foo',
        })
        self._check_messages(response, [
            {'to_addr': addr, 'content':  u'foo'}
            for addr in addresses])

    @inlineCallbacks
    def test_rapidsms_relay_reply(self):
        msg_id, to_addr = 'abc', '+1234'
        yield self.setup_resource(lambda r: 'OK')
        yield self.dispatch(self.mkmsg_in(message_id=msg_id,
                                          from_addr=to_addr))
        response = yield self._call_relay({
            'to_addr': [to_addr],
            'content': 'foo',
            'in_reply_to': msg_id,
        })
        self._check_messages(response, [
            {'to_addr': to_addr, 'content':  u'foo', 'in_reply_to': msg_id}])

    @inlineCallbacks
    def test_rapidsms_relay_reply_unknown_msg(self):
        yield self.setup_resource()
        response = yield self._call_relay({
            'to_addr': ['+123456'],
            'content': 'foo',
            'in_reply_to': 'unknown_message_id',
        })
        self.assertEqual(response.code, 400)
        self.assertEqual(response.delivered_body,
                         "Original message u'unknown_message_id' not found.")
        [err] = self.flushLoggedErrors(BadRequestError)

    @inlineCallbacks
    def test_rapidsms_relay_outbound_authenticated(self):
        auth = ("username", "good-password")
        yield self.setup_resource(callback=None, auth=auth)
        response = yield self._call_relay({
            'to_addr': ['+123456'],
            'content': u'f\xc6r',
        }, auth=auth)
        self._check_messages(response, [
            {'to_addr': '+123456', 'content': u'f\xc6r'}])

    @inlineCallbacks
    def test_rapidsms_relay_outbound_failed_authenticated(self):
        bad_auth = ("username", "bad-password")
        good_auth = ("username", "good-password")
        yield self.setup_resource(callback=None, auth=good_auth)
        response = yield self._call_relay({
            'to_addr': ['+123456'],
            'content': u'f\xc6r',
        }, auth=bad_auth)
        self.assertEqual(response.code, 401)
        self.assertEqual(response.delivered_body, "Unauthorized")
